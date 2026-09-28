using System.Collections;
using System.Collections.Generic;
using UnityEngine;
using UnityEngine.UI;


public class UIMenu_Solo : MonoBehaviour
{
    public bool isSolo;
    public GameObject item;

    ClearSolo clearSolo;
    // Start is called before the first frame update
    void Start()
    {
        clearSolo=FindObjectOfType<ClearSolo>();        
    }

    // Update is called once per frame
    void Update()
    {
        
    }

    public void SetOff(){
        if (isSolo){
            SwitchSolo();
        }
    }

    public void SwitchSolo(){
        isSolo=!isSolo;
        ColorBlock colors = GetComponent<Button>().colors;        
        if (isSolo){            
            colors.normalColor=Color.yellow;
            colors.selectedColor=Color.yellow;
            clearSolo.activeSolos.Add(this);
        }
        else{
            colors.normalColor=Color.gray;
            colors.selectedColor=Color.gray;
            clearSolo.activeSolos.Remove(this);
        }
        GetComponent<Button>().colors=colors;
        
        try{
            item.GetComponent<CustomAudioSource>().isSolo=isSolo;
        }
        catch{
            item.GetComponent<Array>().isSolo=isSolo;
        }
    }
}
