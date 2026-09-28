using System.Collections;
using System.Collections.Generic;
using TMPro;
using UnityEngine;
using UnityEngine.UI;
using System.Globalization;
using StringMath;
using System;

public class Inspector : MonoBehaviour
{
    List<Selectable> selected;

    public TMP_InputField posX,posZ,delayMeters,delayMilliseconds;
    public TMP_InputField objectName;
    public Toggle reversePolarity;
    public Slider attenuation;

    bool updateDataOnNextFrame;
    GameObject contentObject;
    GameObject inputFieldPrefab;

    string oldTextposX="";

    string oldTextposZ="";
    string oldTextDelayInMeters="";
    string oldTextDelayInMilliseconds="";

    // Start is called before the first frame update
    void Awake(){
        inputFieldPrefab = Resources.Load("UI/ArrayDegreeInputField",typeof(GameObject)) as GameObject;
    }
    void Start()
    {
        selected= new List<Selectable>();
        updateDataOnNextFrame=false;
        contentObject = transform.GetChild(0).gameObject;
        contentObject.SetActive(false);
    }

    public void AddToSelection(Selectable sel){
        selected.Add(sel);
        UpdateData();
    }

    public void Deselect(Selectable sel){
        selected.Remove(sel);
        UpdateData();
    }

    public void DeselectAll(){
        selected.Clear();
        UpdateData();
    }

    void LateUpdate()
    {
        if (updateDataOnNextFrame){
            UpdateData();
            updateDataOnNextFrame=false;
        }
    }    

    // Update is called once per frame
    public void UpdateData()
    {         
        if (selected.Count==0){
            contentObject.SetActive(false);
            return;
        }
        contentObject.SetActive(true);

        WriteInfo(selected);
    }
    void WriteInfo(List<Selectable> selection, bool manageMultiple=false){
        string textName="";
        string textType="";
        float floatAttenuation=float.MaxValue;
        string textposX="";
        string textposZ="";
        string textDelayInMeters="";
        string textDelayInMilliseconds="";
        string textPolarity="";
        reversePolarity.gameObject.SetActive(true);
        attenuation.gameObject.SetActive(true);

        foreach (Selectable sel in selection){
            GameObject go = sel.gameObject;
            if (textName==""){
                textName=go.name;
            }
            else if (textName!=go.name){
                textName="?";
            }

            string temp="";
            //type
            try{
                temp=go.GetComponent<CustomAudioSource>().audioFixture.fixtureName;
            }
            catch{
                temp=go.GetComponent<Array>().audioFixture.fixtureName;
            }
            if (textType==""){
                textType=temp;
            }
            else if (textType!=temp){
                textType="?";
            }

            //attenuation
            float tempFloat=0;
            try{
                tempFloat=go.GetComponent<CustomAudioSource>().attenuation;
            }
            catch{
                tempFloat=go.GetComponent<Array>().attenuation;
            }
            if (floatAttenuation==float.MaxValue){
                floatAttenuation=tempFloat;
            }
            else if (floatAttenuation!=tempFloat){
                attenuation.gameObject.SetActive(false);
            }

            if (textposX==""){
                textposX=go.transform.position.x.ToString();
            }
            else if (textposX!=go.transform.position.x.ToString()){
                textposX="?";
            }
            if (textposZ==""){
                textposZ=go.transform.position.z.ToString();
            }
            else if (textposZ!=go.transform.position.z.ToString()){
                textposZ="?";
            }         
            try{
                temp=go.GetComponent<CustomAudioSource>().delayInMeters.ToString();
            }
            catch{
                temp=go.GetComponent<Array>().delayInMeters.ToString();
            }
            if (textDelayInMeters==""){
                textDelayInMeters=temp;
            }
            else if (textDelayInMeters!=temp){
                textDelayInMeters="?";
            }
            try{
                temp=go.GetComponent<CustomAudioSource>().delayInMilliseconds.ToString();
            }
            catch{
                temp=go.GetComponent<Array>().delayInMilliseconds.ToString();
            }
            if (textDelayInMilliseconds==""){
                textDelayInMilliseconds=temp;
            }
            else if (textDelayInMilliseconds!=temp){
                textDelayInMilliseconds="?";
            }
            try{
                temp=go.GetComponent<CustomAudioSource>().reversePolarity.ToString();
            }
            catch{
                temp=go.GetComponent<Array>().reversePolarity.ToString();
            }
            if (textPolarity==""){
                textPolarity=temp;
            }
            else if (textPolarity!=temp){
                textPolarity="?";
            }
        }
        objectName.text=textName;

        posX.text=textposX.ToString();
        posZ.text=textposZ.ToString();

        delayMeters.text=textDelayInMeters;
        delayMilliseconds.text=textDelayInMilliseconds;

        if (textPolarity=="?"){
            reversePolarity.gameObject.SetActive(false);
        }
        else{
            reversePolarity.isOn=bool.Parse(textPolarity);
        }

        if (attenuation.gameObject.activeSelf){
            attenuation.value=floatAttenuation;            
            attenuation.GetComponentInChildren<TMP_Text>().text=floatAttenuation.ToString()+" dB";   
        }

        //update old values
        oldTextposX=textposX;
        oldTextposZ=textposZ;
        oldTextDelayInMeters=textDelayInMeters;
        oldTextDelayInMilliseconds=textDelayInMilliseconds;
    }


    public string ValidateValue(string value, bool nonZeroPositive=false)
    {
        if (value.Length==0){
            return null;
        }
        bool relative = value[0]=='?';
        if (relative){
            value=value.Substring(1);
            if (value.Length==0){
                return null;
            }
            if (value[0]=='+'){
                value=value.Substring(1);
            }
            if (value.Length==0){
                return null;
            }
        }
        value=value.Replace(',','.');
        try{     
            MathExpr expr = value;
            float f = (float) expr.Result;
            if (nonZeroPositive && f<=0){
                return null;
            }
            if (relative){
                return "?"+f.ToString().Replace(',','.');
            }
            return f.ToString().Replace(',','.');            
        }
        catch(MathException exception){
            Debug.Log(exception.Message);
            return null;
        }
    }

    public void ChangeName(string s){
        if (selected.Count==1){
            selected[0].gameObject.name=s;
            return;
        }
        if (s=="?"){
            return;
        }
        int n=1;
        foreach(Selectable sel in selected){
            sel.gameObject.name=s+"_"+n;
            n++;
        }
    }

    public void ChangeAttenuation(Single value){
        foreach(Selectable sel in selected){
            try{
                sel.GetComponent<CustomAudioSource>().attenuation=value;
            }
            catch{
                sel.GetComponent<Array>().attenuation=value;
            }
        }
        updateDataOnNextFrame=true;
        attenuation.GetComponentInChildren<TMP_Text>().text=value.ToString()+" dB";   
    }
    public void ChangePosX(string s){
        
        string validated = ValidateValue(s);
        if (validated==null){
            posX.text=oldTextposX;
            return;
        }
        bool relative = validated[0]=='?';
        if (relative){
            validated=validated.Substring(1);
        }
        float value = float.Parse(validated,CultureInfo.InvariantCulture);
        
        foreach(Selectable sel in selected){
            if (relative){
                sel.transform.position=new Vector3(sel.transform.position.x+value,sel.transform.position.y,sel.transform.position.z);
            }
            else{
                sel.transform.position=new Vector3(value,sel.transform.position.y,sel.transform.position.z);
            }
        }
        oldTextposX=s;
    }
    public void ChangePosZ(string s){
        string validated = ValidateValue(s);
        if (validated==null){
            posZ.text=oldTextposZ;
            return;
        }
        bool relative = validated[0]=='?';
        if (relative){
            validated=validated.Substring(1);
        }
        float value = float.Parse(validated,CultureInfo.InvariantCulture);
        
        foreach(Selectable sel in selected){
            if (relative){
                sel.transform.position=new Vector3(sel.transform.position.x,sel.transform.position.y,sel.transform.position.z+value);
            }
            else{
                sel.transform.position=new Vector3(sel.transform.position.x,sel.transform.position.y,value);
            }
        }
        oldTextposZ=s;
    }
    
    public void ChangeDelayMeters(string s){
        string validated = ValidateValue(s);
        if (validated==null){
            delayMeters.text=oldTextDelayInMeters;
            delayMilliseconds.text=oldTextDelayInMilliseconds;
            return;
        }
        bool relative = validated[0]=='?';
        if (relative){
            validated=validated.Substring(1);
        }
        float value = float.Parse(validated,CultureInfo.InvariantCulture);
        foreach(Selectable sel in selected){
            if (relative){
                try{
                    sel.GetComponent<CustomAudioSource>().delayInMeters+=value;
                }
                catch{
                    sel.GetComponent<Array>().delayInMeters+=value;
                }
            }
            else{
                try{
                    sel.GetComponent<CustomAudioSource>().delayInMeters=value;
                }
                catch{
                    sel.GetComponent<Array>().delayInMeters=value;
                }
            }
        }
        updateDataOnNextFrame=true;
        oldTextDelayInMeters=s;
    }
    public void ChangeDelayMilliseconds(string s){
        string validated = ValidateValue(s);
        if (validated==null){
            delayMeters.text=oldTextDelayInMeters;
            delayMilliseconds.text=oldTextDelayInMilliseconds;
            return;
        }
        bool relative = validated[0]=='?';
        if (relative){
            validated=validated.Substring(1);
        }
        float value = float.Parse(validated,CultureInfo.InvariantCulture);
        foreach(Selectable sel in selected){
            if (relative){
                try{
                    sel.GetComponent<CustomAudioSource>().delayInMilliseconds+=value;              
                }
                catch{
                    sel.GetComponent<Array>().delayInMilliseconds+=value;
                }
            }
            else{
                try{
                    sel.GetComponent<CustomAudioSource>().delayInMilliseconds=value;
                }
                catch{
                    sel.GetComponent<Array>().delayInMilliseconds=value;
                }
            }
        }
        updateDataOnNextFrame=true;
        oldTextDelayInMilliseconds=s;
    }
    public void ChangeReversePolarity(bool b){
        if (selected.Count!=1){
            return;
        }
        try{
            selected[0].GetComponent<CustomAudioSource>().reversePolarity=b;
        }
        catch{
            selected[0].GetComponent<Array>().reversePolarity=b;
        }
    }    
}
