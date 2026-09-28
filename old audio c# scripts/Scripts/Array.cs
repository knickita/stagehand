using System.Collections;
using System.Collections.Generic;
using System.Linq;
using UnityEngine;

[ExecuteAlways]
public class Array : MonoBehaviour
{   
    [Header("Array Configuration")]
    public float separation;
    public float[] degrees;

    [Header("Audio Fixture")]
    public AudioFixture audioFixture;

    public float delayInMeters,delayInMilliseconds;
    public bool reversePolarity;

    float oldDelayInMeters, oldDelayInMilliseconds;    

    AudioManager manager;

    public bool muted;
    public bool isSolo;
    public float attenuation;

    // Start is called before the first frame update
    void Start()
    {
        manager = FindObjectOfType<AudioManager>();        
        oldDelayInMeters=delayInMeters;
        oldDelayInMilliseconds=delayInMilliseconds;
        UpdateSources();
    }

    // Update is called once per frame
    void Update()
    {        
        //permette di cambiare uno qualunque dei due campi, e corregge l'altro di conseguenza
        if (oldDelayInMeters!=delayInMeters){
            oldDelayInMeters=delayInMeters;
            delayInMilliseconds=delayInMeters*1000f/343f;
            oldDelayInMilliseconds=delayInMilliseconds;
        }
        if (oldDelayInMilliseconds!=delayInMilliseconds){
            oldDelayInMilliseconds=delayInMilliseconds;
            delayInMeters=delayInMilliseconds*343f/1000f;
            oldDelayInMeters=delayInMeters;
        }

        if (separation<0){
            separation=0;
        }
        UpdateSourcesData();
    }

    public void ChangeNumberOfElements(int newNumber){
        int oldNumber = degrees.Length;
        if (newNumber==oldNumber){
            return;
        }
        if (newNumber>oldNumber){
            for (int i = 0; i < newNumber-oldNumber; i++)
            {
                degrees=degrees.Concat(new float[]{0}).ToArray();
            }
        }
        else{
            degrees=degrees.Take(newNumber).ToArray();
        }
        UpdateSources();
    }

    void UpdateSources(){
        while (transform.childCount>0){
            manager.audioSources.Remove(transform.GetChild(0).gameObject.GetComponent<CustomAudioSource>());
            DestroyImmediate(transform.GetChild(0).gameObject);
        }
        for (int i = 0; i < degrees.Length; i++)
        {
            AddAudioSource();
        }        
    }

    void UpdateSourcesData(){
        float[] summedDegrees = new float[degrees.Length];
        summedDegrees[0]=degrees[0];
        for (int i = 1; i < degrees.Length; i++)
        {
            summedDegrees[i]=degrees[i]+summedDegrees[i-1];
        }
        for (int index=0;index<transform.childCount;index++){
            transform.GetChild(index).localScale= Vector3.one*separation;
            transform.GetChild(index).localRotation = Quaternion.Euler(summedDegrees[index],0,0);
            transform.GetChild(index).localPosition = Vector3.zero;
            CustomAudioSource source = transform.GetChild(index).GetComponent<CustomAudioSource>();
            source.audioFixture=audioFixture;
            //accoppia gli angoli posteriori delle casse
            if (index>0){
                Transform transformUp = transform.GetChild(index-1);
                Vector3 upPoint=transformUp.position+Vector3.Scale(transformUp.rotation*new Vector3(0,-1,-1)/2,transformUp.localScale);
                Transform transformDown = transform.GetChild(index);
                Vector3 downPoint=transformDown.position+Vector3.Scale(transformDown.rotation*new Vector3(0,1,-1)/2,transformDown.localScale);
                Vector3 translation=upPoint-downPoint;
                transformDown.Translate(translation,Space.World);
            }
        }
        //reset the scales
        for (int index=0;index<transform.childCount;index++){
            transform.GetChild(index).localScale= Vector3.one;
        }
    }

    void AddAudioSource(){
        GameObject newSource = Resources.Load("AudioSource", typeof(GameObject)) as GameObject;
        newSource=Instantiate(newSource,transform);
        newSource.transform.localPosition = Vector3.zero;
        newSource.transform.localRotation = Quaternion.identity;
        newSource.transform.localScale=Vector3.one;
        newSource.GetComponent<CustomAudioSource>().parentArray=this;
        manager.audioSources.Add(newSource.GetComponent<CustomAudioSource>());
    }
}
