using System.Collections;
using System.Collections.Generic;
using UnityEngine;
using UnityEngine.UI;

public class ClearSolo : MonoBehaviour
{
    public List<UIMenu_Solo> activeSolos;
    // Start is called before the first frame update
    void Start()
    {
        activeSolos= new List<UIMenu_Solo>();
    }

    void Update(){
        ColorBlock colors = GetComponent<Button>().colors;
        if (activeSolos.Count>0){
            float factor = Mathf.Sin(Time.realtimeSinceStartup);
            factor=Mathf.Abs(factor)/2.0f+0.5f;
            colors.normalColor=Color.yellow*factor;
            colors.selectedColor=Color.yellow*factor;
        }
        else{
            colors.normalColor=Color.gray;
            colors.selectedColor=Color.gray;
        }        
        GetComponent<Button>().colors=colors;
    }

    public void Clear(){
        while (activeSolos.Count>0)
        {
            activeSolos[0].SwitchSolo();
        }
    }
}
